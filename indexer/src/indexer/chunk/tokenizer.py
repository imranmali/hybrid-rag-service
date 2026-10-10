"""The bge-m3 tokenizer adapter used for all chunk boundaries."""

from functools import cached_property

from tokenizers import Tokenizer


class BgeM3Tokenizer:
    """Counts and slices text without inserting model special tokens."""

    model_name = "BAAI/bge-m3"

    @cached_property
    def tokenizer(self) -> Tokenizer:
        return Tokenizer.from_pretrained(self.model_name)

    def encode(self, text: str) -> list[int]:
        return self.tokenizer.encode(text, add_special_tokens=False).ids

    def count(self, text: str) -> int:
        return len(self.encode(text))

    def decode(self, token_ids: list[int]) -> str:
        return self.tokenizer.decode(token_ids, skip_special_tokens=True).strip()
