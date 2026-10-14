"""Unit tests for chunking and vector formatting. No database or Ollama needed."""

from rag.embeddings import EMBED_DIMENSIONS, EMBED_MODEL, to_pgvector
from rag.retrieval import DEFAULT_K
from rag.index_docs import CHUNK_SIZE, document_title, split_text, text_to_embed

SENTENCE = "Stop the lot and attach a red HOLD tag to every affected panel. "
LONG_TEXT = "\n\n".join(SENTENCE * 4 for _ in range(10))


def test_empty_text_gives_no_chunks():
    assert split_text("   \n ") == []


def test_short_text_is_one_chunk():
    assert split_text("  Short instruction.  ") == ["Short instruction."]


def test_prose_chunks_respect_the_size_limit():
    chunks = split_text(LONG_TEXT)
    assert len(chunks) > 1
    assert all(len(chunk) <= CHUNK_SIZE for chunk in chunks)


def test_fallback_split_keeps_sentences_whole():
    # No headings and no blank lines, so only the size fallback applies.
    chunks = split_text(SENTENCE * 30)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.startswith("Stop the lot")
        assert chunk.endswith("panel.")
        assert "tag," not in chunk or "HOLD tag to every" in chunk


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


def test_sections_split_on_markdown_headings():
    text = "# Title\n\nIntro.\n\n## First\n\nAlpha.\n\n## Second\n\nBeta."
    chunks = split_text(text)
    assert chunks[0].startswith("# Title")
    assert any(chunk.startswith("## First") and "Alpha." in chunk for chunk in chunks)
    assert any(chunk.startswith("## Second") and "Beta." in chunk for chunk in chunks)
    assert not any("Alpha." in chunk and "Beta." in chunk for chunk in chunks)


def test_numbered_list_stays_in_one_chunk_even_past_the_size_limit():
    steps = "\n".join(f"{number}. " + ("action " * 20) for number in range(1, 8))
    text = f"# Work\n\n## Immediate actions\n\n{steps}\n\n## Later\n\nDone."
    assert len(steps) > CHUNK_SIZE
    chunks = split_text(text)
    lists = [chunk for chunk in chunks if "1. action" in chunk]
    assert len(lists) == 1
    assert "7. action" in lists[0]
    assert "5. action" in lists[0]


def test_order_hold_release_is_not_split_mid_sentence():
    text = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "docs"
        / "instructions"
        / "order-hold-and-release.md"
    ).read_text(encoding="utf-8")
    chunks = split_text(text)
    sentence = "remove the HOLD tag, and record who released the order and why."
    assert any(sentence in chunk for chunk in chunks)
    for chunk in chunks:
        assert "tag, and record who" not in chunk.split("\n", 1)[-1][:20]
        body = chunk
        for line in chunk.splitlines():
            if line.startswith("#"):
                continue
            body = line.strip()
            break
        if body.startswith("- "):
            body = body[2:]
        assert not body[:1].islower()


def test_delamination_immediate_actions_include_step_5():
    text = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "docs"
        / "instructions"
        / "delamination-containment.md"
    ).read_text(encoding="utf-8")
    chunks = split_text(text)
    immediate = [chunk for chunk in chunks if "Immediate actions" in chunk]
    assert len(immediate) == 1
    for step in ("1. Stop", "2. Attach", "3. Record", "4. Set", "5. Notify"):
        assert step in immediate[0]


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
