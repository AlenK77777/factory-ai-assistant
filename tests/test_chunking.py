"""Unit tests for chunking and vector formatting. No database or Ollama needed."""

from rag.embeddings import EMBED_DIMENSIONS, EMBED_MODEL, to_pgvector
from rag.retrieval import DEFAULT_K
from rag.index_docs import CHUNK_OVERLAP, CHUNK_SIZE, document_title, split_text, text_to_embed

SENTENCE = "Stop the lot and attach a red HOLD tag to every affected panel. "
LONG_TEXT = "\n\n".join(SENTENCE * 4 for _ in range(10))


def test_empty_text_gives_no_chunks():
    assert split_text("   \n ") == []


def test_short_text_is_one_chunk():
    assert split_text("  Short instruction.  ") == ["Short instruction."]


def test_chunks_are_not_longer_than_the_limit():
    chunks = split_text(LONG_TEXT)
    assert len(chunks) > 1
    assert all(len(chunk) <= CHUNK_SIZE for chunk in chunks)


def test_chunks_overlap():
    chunks = split_text(LONG_TEXT)
    for previous, current in zip(chunks, chunks[1:]):
        assert current[:10] in previous[-CHUNK_OVERLAP - 10 :]


def test_no_text_is_lost():
    chunks = split_text(LONG_TEXT)
    words = set(" ".join(chunks).split())
    assert words == set(LONG_TEXT.split())


def test_chunks_end_on_whole_words():
    text = " ".join(f"word{i}" for i in range(300))
    for chunk in split_text(text):
        assert chunk.split()[-1].startswith("word")
        assert all(token.startswith("word") and token[4:].isdigit() for token in chunk.split())


def test_text_without_spaces_is_still_split():
    chunks = split_text("x" * 1200)
    assert all(len(chunk) <= CHUNK_SIZE for chunk in chunks)
    assert len(chunks) == 3


def test_document_title_uses_first_heading():
    assert document_title("> note\n# WI-1 Title\n## Sub", fallback="file") == "WI-1 Title"
    assert document_title("no heading here", fallback="file") == "file"


def test_title_is_added_to_mid_document_chunks_only():
    assert text_to_embed("WI-1 Title", "Step 3: retest.") == "WI-1 Title\nStep 3: retest."
    assert text_to_embed("WI-1 Title", "# WI-1 Title\nIntro") == "# WI-1 Title\nIntro"


def test_pgvector_text_format():
    assert to_pgvector([0.5, -1, 2.25]) == "[0.5,-1.0,2.25]"


def test_embedding_model_is_multilingual_1024():
    assert EMBED_MODEL == "qwen3-embedding:0.6b"
    assert EMBED_DIMENSIONS == 1024


def test_default_k_is_six():
    assert DEFAULT_K == 6
