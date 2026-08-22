"""Windowing helpers for large document text.

Extracted minutes text can be hundreds of KB; tools must return bounded
slices. Callers page through the text via ``offset``/``next_offset``.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TextWindow:
    """A bounded slice of a larger text."""

    text: str
    offset: int
    text_length: int
    has_more: bool
    next_offset: int | None


def window(text: str | None, offset: int = 0, max_chars: int = 20_000) -> TextWindow:
    """Return a window of at most ``max_chars`` code points starting at ``offset``.

    ``None`` text behaves as the empty string. Slicing is by Python string
    index (code points); no attempt is made to align to word boundaries.
    An ``offset`` at or past the end of the text yields an empty window.

    Raises:
        ValueError: if ``offset`` is negative or ``max_chars`` < 1.
    """
    if offset < 0:
        raise ValueError(f"offset must be >= 0, got {offset}")
    if max_chars < 1:
        raise ValueError(f"max_chars must be >= 1, got {max_chars}")
    full = text or ""
    chunk = full[offset : offset + max_chars]
    has_more = offset + len(chunk) < len(full)
    return TextWindow(
        text=chunk,
        offset=offset,
        text_length=len(full),
        has_more=has_more,
        next_offset=offset + len(chunk) if has_more else None,
    )
