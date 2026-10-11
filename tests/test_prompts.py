import tiktoken

from probe.config import SIZES, prompt


def test_reference_prompt_sizes():
    encoding = tiktoken.get_encoding("cl100k_base")
    for size in SIZES:
        assert len(encoding.encode(prompt(size))) == size
