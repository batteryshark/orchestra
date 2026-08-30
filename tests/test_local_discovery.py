"""Local inference-server discovery with every probe isolated from the host."""
import unittest

from orchestra import profiles

OLLAMA_TAGS = {"models": [{"name": "qwen3:14b"}, {"name": "llama4:8b"}]}
OPENAI_MODELS = {"data": [{"id": "gpt-oss-20b"}]}

class ProbeTests(unittest.TestCase):
    def test_probe_parses_each_server_shape(self) -> None:
        found = profiles.discover_local(
            fetch=lambda url: OLLAMA_TAGS if "11434" in url else OPENAI_MODELS)
        self.assertEqual(found, [
            {"id": "qwen3:14b", "source": "ollama"},
            {"id": "llama4:8b", "source": "ollama"},
            {"id": "gpt-oss-20b", "source": "lmstudio"},
            {"id": "gpt-oss-20b", "source": "vllm"},
        ])

    def test_all_probes_dead_yield_nothing(self) -> None:
        def refused(_url):
            raise OSError("connection refused")
        self.assertEqual(profiles.discover_local(fetch=refused), [])

    def test_partial_and_malformed_answers_fail_soft(self) -> None:
        def fetch(url):
            if "11434" in url:
                return {"models": [{"name": "qwen3:14b"}, {"nope": 1}, "junk"]}
            if "1234" in url:
                raise OSError("refused")
            return {"data": "not-a-list"}
        self.assertEqual(profiles.discover_local(fetch=fetch),
                         [{"id": "qwen3:14b", "source": "ollama"}])

if __name__ == "__main__":
    unittest.main()
