"""Unit tests for ah.core.text_search.build_or_tsquery."""
from __future__ import annotations

import pytest

from ah.core.text_search import build_or_tsquery


class TestBuildOrTsquery:
    """Tests for the sanitized OR tsquery builder."""

    def test_empty_string_returns_empty(self):
        assert build_or_tsquery("") == ""

    def test_whitespace_only_returns_empty(self):
        assert build_or_tsquery("   \t\n  ") == ""

    def test_all_stopwords_returns_empty(self):
        assert build_or_tsquery("the and or a an") == ""

    def test_single_word(self):
        assert build_or_tsquery("hello") == "hello"

    def test_multiple_words_or_joined(self):
        result = build_or_tsquery("hello world")
        assert result == "hello | world"

    def test_case_insensitive(self):
        result = build_or_tsquery("Hello WORLD")
        assert result == "hello | world"

    def test_deduplicates_repeated_words(self):
        result = build_or_tsquery("hello hello world world")
        assert result == "hello | world"

    def test_stopwords_removed(self):
        result = build_or_tsquery("the quick brown fox")
        assert result == "quick | brown | fox"

    def test_mixed_stopwords_and_content(self):
        result = build_or_tsquery("where did the quartzotter migration happen")
        assert result == "quartzotter | migration | happen"

    def test_special_characters_stripped(self):
        # Characters like & | ! ( ) : * ' should not appear in output
        result = build_or_tsquery("hello & world | foo ! bar")
        assert result == "hello | world | foo | bar"

    def test_punctuation_stripped(self):
        result = build_or_tsquery("hello, world! how are you?")
        assert result == "hello | world | you"

    def test_numbers_preserved(self):
        result = build_or_tsquery("error 404 not found")
        assert result == "error | 404 | not | found"

    def test_numbers_and_words(self):
        result = build_or_tsquery("python3 is better than python2")
        assert result == "python3 | better | than | python2"

    def test_sixteen_term_cap_enforced(self):
        words = [f"word{i}" for i in range(20)]
        result = build_or_tsquery(" ".join(words))
        terms = result.split(" | ")
        assert len(terms) == 16
        assert terms[0] == "word0"
        assert terms[15] == "word15"

    def test_exactly_sixteen_terms(self):
        words = [f"word{i}" for i in range(16)]
        result = build_or_tsquery(" ".join(words))
        terms = result.split(" | ")
        assert len(terms) == 16

    def test_fifteen_terms_under_cap(self):
        words = [f"word{i}" for i in range(15)]
        result = build_or_tsquery(" ".join(words))
        terms = result.split(" | ")
        assert len(terms) == 15

    def test_cap_with_stopwords_removed_first(self):
        # 20 content words + stopwords interleaved; cap applies after stopword removal
        words = []
        for i in range(20):
            words.append(f"word{i}")
            words.append("the")
        result = build_or_tsquery(" ".join(words))
        terms = result.split(" | ")
        assert len(terms) == 16

    def test_hyphenated_words_split(self):
        result = build_or_tsquery("state-of-the-art design")
        assert result == "state | art | design"

    def test_underscore_not_included(self):
        result = build_or_tsquery("hello_world test")
        assert result == "hello | world | test"

    def test_unicode_letters_stripped(self):
        # Non-ASCII letters are not matched by [a-z0-9] after lower()
        # "café" -> "caf", "résumé" -> "r" + "sum" (é stripped, r remains)
        result = build_or_tsquery("café résumé")
        assert result == "caf | r | sum"

    def test_empty_after_stopword_removal(self):
        assert build_or_tsquery("the a an and or") == ""

    def test_single_stopword(self):
        assert build_or_tsquery("the") == ""

    def test_query_with_only_special_chars(self):
        assert build_or_tsquery("!@#$%^&*()") == ""

    def test_preserves_order_of_first_appearance(self):
        result = build_or_tsquery("zebra apple mango apple zebra")
        assert result == "zebra | apple | mango"

    def test_long_query_truncated_to_16_terms(self):
        # 50 unique words
        words = [f"term{i:02d}" for i in range(50)]
        result = build_or_tsquery(" ".join(words))
        terms = result.split(" | ")
        assert len(terms) == 16
        assert terms == [f"term{i:02d}" for i in range(16)]
