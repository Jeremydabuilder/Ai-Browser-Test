"""Phase 13 (Semantic History / Local RAG): the non-Qt core - embeddings,
chunking, the storage layer, KnowledgeIndex, and retrieval. See
test_knowledge_integration.py for the MainWindow/UI wiring.

Run with:
    python -m unittest tests.test_knowledge_core -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.knowledge import chunking, embeddings, retrieval  # noqa: E402
from app.knowledge.index import KnowledgeIndex, url_source_id  # noqa: E402
from app.knowledge.types import SourceType  # noqa: E402
from app.storage import Database, KnowledgeStore  # noqa: E402
from app.storage.settings import SettingsStore  # noqa: E402


class EmbeddingTests(unittest.TestCase):
    def test_embeddings_are_unit_length_or_zero(self) -> None:
        import math

        vector = embeddings.embed("some ordinary sentence about MCP permissions")
        norm = math.sqrt(sum(v * v for v in vector))
        self.assertAlmostEqual(norm, 1.0, places=6)

    def test_empty_text_embeds_to_the_zero_vector(self) -> None:
        vector = embeddings.embed("")
        self.assertTrue(all(v == 0.0 for v in vector))
        self.assertEqual(embeddings.cosine_similarity(vector, vector), 0.0)

    def test_related_text_scores_higher_than_unrelated_text(self) -> None:
        query = embeddings.embed("MCP permissions and OAuth security")
        related = embeddings.embed("OAuth security tokens for MCP servers")
        unrelated = embeddings.embed("a recipe for chocolate chip cookies")
        self.assertGreater(
            embeddings.cosine_similarity(query, related),
            embeddings.cosine_similarity(query, unrelated))

    def test_content_hash_is_stable_and_sensitive_to_change(self) -> None:
        self.assertEqual(embeddings.content_hash("hello"), embeddings.content_hash("hello"))
        self.assertNotEqual(embeddings.content_hash("hello"), embeddings.content_hash("hellO"))


class ChunkingTests(unittest.TestCase):
    def test_paragraphs_are_grouped_up_to_the_size_cap(self) -> None:
        chunks = chunking.chunk_paragraphs("first paragraph.\n\nsecond paragraph.",
                                           max_chars=1000)
        self.assertEqual(len(chunks), 1)
        self.assertIn("first paragraph", chunks[0])
        self.assertIn("second paragraph", chunks[0])

    def test_an_oversized_paragraph_becomes_its_own_chunk_rather_than_split(self) -> None:
        long_paragraph = "x" * 2000
        chunks = chunking.chunk_paragraphs(f"short one.\n\n{long_paragraph}", max_chars=500)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[1], long_paragraph)

    def test_a_highlight_is_exactly_one_chunk(self) -> None:
        chunks = chunking.chunk_highlight("a short highlighted passage")
        self.assertEqual(chunks, ["a short highlighted passage"])

    def test_a_blank_highlight_produces_no_chunks(self) -> None:
        self.assertEqual(chunking.chunk_highlight("   "), [])

    def test_mission_goal_and_result_are_separate_labeled_chunks(self) -> None:
        chunks = chunking.chunk_mission("find shoes", "nike air max wins")
        self.assertEqual(chunks, [("goal", "find shoes"), ("result", "nike air max wins")])

    def test_a_mission_with_no_result_yet_only_yields_a_goal_chunk(self) -> None:
        self.assertEqual(chunking.chunk_mission("find shoes", ""), [("goal", "find shoes")])

    def test_pdf_pages_are_chunked_independently_and_page_tagged(self) -> None:
        pages = ["page one text.", "page two text.\n\n" + "y" * 2000]
        result = chunking.chunk_pdf(pages)
        page_numbers = [page for page, _text in result]
        self.assertIn(1, page_numbers)
        self.assertIn(2, page_numbers)


def _make() -> tuple[KnowledgeIndex, KnowledgeStore, SettingsStore, Database, object]:
    tmp = tempfile.TemporaryDirectory()
    db = Database(os.path.join(tmp.name, "t.sqlite3"))
    store = KnowledgeStore(db)
    settings = SettingsStore(db)
    index = KnowledgeIndex(store, settings)
    return index, store, settings, db, tmp


class OptInTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index, self.store, self.settings, self.db, self._tmp = _make()

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_disabled_by_default(self) -> None:
        self.assertFalse(self.settings.semantic_history_enabled)
        self.assertFalse(self.index.enabled)

    def test_indexing_is_a_no_op_while_disabled(self) -> None:
        self.index.index_history_visit("https://example.com", "Example")
        self.index.index_highlight(1, "some highlighted text")
        self.index.index_mission(1, "a goal", "a result")
        self.assertEqual(self.store.count(), 0)

    def test_enabling_lets_indexing_proceed(self) -> None:
        self.settings.semantic_history_enabled = True
        self.index.index_history_visit("https://example.com", "Example")
        self.assertGreater(self.store.count(), 0)


class HistoryIndexingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index, self.store, self.settings, self.db, self._tmp = _make()
        self.settings.semantic_history_enabled = True

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_indexes_title_and_url_as_metadata(self) -> None:
        self.index.index_history_visit("https://example.com/mcp", "MCP Article",
                                       "2020-01-01T00:00:00+00:00")
        chunks = self.store.chunks_for_source(SourceType.HISTORY, url_source_id(
            "https://example.com/mcp"))
        self.assertEqual(len(chunks), 1)
        self.assertIn("MCP Article", chunks[0].text)
        self.assertIn("https://example.com/mcp", chunks[0].text)

    def test_revisiting_the_same_url_updates_one_chunk_not_two(self) -> None:
        self.index.index_history_visit("https://example.com", "First Title")
        self.index.index_history_visit("https://example.com", "First Title")
        self.assertEqual(self.store.count(), 1)

    def test_provenance_is_carried_on_every_chunk(self) -> None:
        self.index.index_history_visit("https://example.com/page", "A Page",
                                       "2024-06-01T00:00:00+00:00")
        chunk = self.store.all_chunks()[0]
        self.assertEqual(chunk.source_type, SourceType.HISTORY)
        self.assertEqual(chunk.location, "https://example.com/page")
        self.assertEqual(chunk.timestamp, "2024-06-01T00:00:00+00:00")
        self.assertTrue(chunk.title)


class UnchangedContentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index, self.store, self.settings, self.db, self._tmp = _make()
        self.settings.semantic_history_enabled = True

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_reindexing_identical_text_does_not_touch_the_row(self) -> None:
        self.index.index_highlight(1, "some highlighted text", title="A")
        before = self.store.all_chunks()[0]
        self.index.index_highlight(1, "some highlighted text", title="A")
        after = self.store.all_chunks()[0]
        self.assertEqual(before.created_at, after.created_at)
        self.assertEqual(self.store.count(), 1)

    def test_reindexing_changed_text_does_update(self) -> None:
        self.index.index_highlight(1, "original text")
        self.index.index_highlight(1, "changed text")
        chunk = self.store.chunks_for_source(SourceType.HIGHLIGHT, "1")[0]
        self.assertEqual(chunk.text, "changed text")

    def test_a_shrunk_mission_drops_its_extra_chunk(self) -> None:
        self.index.index_mission(1, "goal text", "result text")
        self.assertEqual(len(self.store.chunks_for_source(SourceType.MISSION, "1")), 2)
        self.index.index_mission(1, "goal text", "")  # result cleared
        self.assertEqual(len(self.store.chunks_for_source(SourceType.MISSION, "1")), 1)


class MissionFindingIndexingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index, self.store, self.settings, self.db, self._tmp = _make()
        self.settings.semantic_history_enabled = True

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_indexes_a_finding_with_its_mission_as_parent(self) -> None:
        self.index.index_mission_finding(10, 1, "OAuth tokens should be short-lived")
        chunk = self.store.chunks_for_source(SourceType.MISSION_FINDING, "10")[0]
        self.assertEqual(chunk.parent_id, "1")
        self.assertEqual(chunk.text, "OAuth tokens should be short-lived")

    def test_removing_a_mission_removes_its_findings_too(self) -> None:
        self.index.index_mission(1, "goal", "result")
        self.index.index_mission_finding(10, 1, "a finding")
        self.index.index_mission_finding(11, 1, "another finding")
        self.assertEqual(self.store.count(), 4)
        self.index.remove_mission(1)
        self.assertEqual(self.store.count(), 0)

    def test_removing_one_finding_does_not_touch_others(self) -> None:
        self.index.index_mission_finding(10, 1, "finding A")
        self.index.index_mission_finding(11, 1, "finding B")
        self.index.remove_mission_finding(10)
        remaining = self.store.all_chunks()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].source_id, "11")


class HighlightIndexingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index, self.store, self.settings, self.db, self._tmp = _make()
        self.settings.semantic_history_enabled = True

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_indexes_one_chunk_with_location(self) -> None:
        self.index.index_highlight(5, "highlighted passage", title="Doc",
                                   location="https://example.com/doc")
        chunk = self.store.chunks_for_source(SourceType.HIGHLIGHT, "5")[0]
        self.assertEqual(chunk.location, "https://example.com/doc")

    def test_removing_a_highlight_removes_its_chunk(self) -> None:
        self.index.index_highlight(5, "highlighted passage")
        self.index.remove_highlight(5)
        self.assertEqual(self.store.count(), 0)


class PdfIndexingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index, self.store, self.settings, self.db, self._tmp = _make()
        self.settings.semantic_history_enabled = True

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_indexes_each_page_with_a_page_tagged_location(self) -> None:
        self.index.index_pdf("/tmp/doc.pdf", ["page one about OAuth.", "page two about MCP."],
                             title="My PDF")
        chunks = self.store.chunks_for_source(SourceType.PDF, "/tmp/doc.pdf")
        self.assertEqual(len(chunks), 2)
        self.assertTrue(chunks[0].location.endswith("#page=1"))
        self.assertTrue(chunks[1].location.endswith("#page=2"))


class FileIndexingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index, self.store, self.settings, self.db, self._tmp = _make()
        self.settings.semantic_history_enabled = True

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_indexes_file_text_by_path(self) -> None:
        self.index.index_file("/tmp/notes.txt", "some file content here", title="notes.txt")
        chunks = self.store.chunks_for_source(SourceType.FILE, "/tmp/notes.txt")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].title, "notes.txt")


class RetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index, self.store, self.settings, self.db, self._tmp = _make()
        self.settings.semantic_history_enabled = True

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_semantic_and_lexical_scoring_rank_relevant_content_first(self) -> None:
        self.index.index_highlight(1, "MCP permissions are scoped per client", title="A")
        self.index.index_highlight(2, "a recipe for chocolate chip cookies", title="B")
        chunks = self.store.all_chunks()
        results = retrieval.search(chunks, "MCP permission scoping")
        self.assertEqual(results[0].chunk.source_id, "1")

    def test_an_empty_query_returns_no_results(self) -> None:
        self.index.index_highlight(1, "some text")
        chunks = self.store.all_chunks()
        self.assertEqual(retrieval.search(chunks, ""), [])

    def test_lexical_overlap_alone_can_still_surface_a_result(self) -> None:
        """A pure keyword hit should not depend on the hashed embedding
        happening to agree - see the lexical half of the blended score."""
        self.index.index_highlight(1, "PyBrowser supports the xyzzy-protocol setting")
        chunks = self.store.all_chunks()
        results = retrieval.search(chunks, "xyzzy-protocol")
        self.assertTrue(results)

    def test_results_carry_provenance_fields(self) -> None:
        self.index.index_highlight(1, "some text", title="Title", location="https://x.example")
        chunks = self.store.all_chunks()
        result = retrieval.search(chunks, "some text")[0]
        self.assertEqual(result.chunk.title, "Title")
        self.assertEqual(result.chunk.location, "https://x.example")
        self.assertEqual(result.chunk.source_type, SourceType.HIGHLIGHT)

    def test_old_content_is_flagged_stale(self) -> None:
        self.index.index_history_visit("https://old.example", "Old Page",
                                       "2000-01-01T00:00:00+00:00")
        chunks = self.store.all_chunks()
        result = retrieval.search(chunks, "old page")[0]
        self.assertTrue(result.stale)

    def test_recent_content_is_not_flagged_stale(self) -> None:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.index.index_history_visit("https://new.example", "New Page", now)
        chunks = self.store.all_chunks()
        result = retrieval.search(chunks, "new page")[0]
        self.assertFalse(result.stale)

    def test_source_type_filter_excludes_other_types(self) -> None:
        self.index.index_highlight(1, "shared keyword content")
        self.index.index_history_visit("https://example.com", "shared keyword content")
        chunks = self.store.all_chunks()
        results = retrieval.search(chunks, "shared keyword", source_types=(SourceType.HIGHLIGHT,))
        self.assertTrue(all(r.chunk.source_type == SourceType.HIGHLIGHT for r in results))


class ClearAndStatsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index, self.store, self.settings, self.db, self._tmp = _make()
        self.settings.semantic_history_enabled = True

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_clear_removes_everything_even_while_disabled(self) -> None:
        self.index.index_highlight(1, "some text")
        self.settings.semantic_history_enabled = False
        self.index.clear()
        self.assertEqual(self.store.count(), 0)

    def test_stats_reports_counts_and_last_indexed(self) -> None:
        self.index.index_highlight(1, "some text")
        stats = self.index.stats()
        self.assertEqual(stats["chunk_count"], 1)
        self.assertEqual(stats["source_count"], 1)
        self.assertIsNotNone(stats["last_indexed_at"])
        self.assertGreater(stats["storage_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
