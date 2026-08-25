"""Local inference server discovery (W-0306 idea 3) and its picker payload.

Every probe is mocked — no test needs a real Ollama. The one test that does
touch the network points every probe at a port that was just closed, proving
discovery stays silent and fast on a machine with no local servers.
"""
import socket
import unittest
from pathlib import Path
from unittest import mock

from orchestra import profile_edit, profiles

OLLAMA_TAGS = {"models": [{"name": "qwen3:14b"}, {"name": "llama4:8b"}]}
OPENAI_MODELS = {"data": [{"id": "gpt-oss-20b"}]}


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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
        # Real HTTP against a just-closed localhost port: the contract is
        # silence, never an error, on a machine with no local servers.
        port = closed_port()
        servers = tuple((name, f"http://127.0.0.1:{port}/x")
                        for name, _ in profiles.LOCAL_SERVERS)
        self.assertEqual(profiles.discover_local(servers=servers), [])

    def test_partial_and_malformed_answers_fail_soft(self) -> None:
        def fetch(url):
            if "11434" in url:
                return {"models": [{"name": "qwen3:14b"}, {"nope": 1}, "junk"]}
            if "1234" in url:
                raise OSError("refused")
            return {"data": "not-a-list"}
        self.assertEqual(profiles.discover_local(fetch=fetch),
                         [{"id": "qwen3:14b", "source": "ollama"}])


class PickerPayloadTests(unittest.TestCase):
    def test_local_models_join_the_options_payload_marked(self) -> None:
        opts = profile_edit.picker_options(
            {}, local=[{"id": "qwen3:14b", "source": "ollama"}])
        self.assertTrue(opts["local"]["local"])
        self.assertEqual(opts["local"]["models"], [
            {"id": "qwen3:14b", "efforts": [], "local": True,
             "source": "ollama"}])
        # `local` is a discovery source, never a harness.
        self.assertNotIn("local", profile_edit.BACKENDS)

    def test_no_local_servers_means_nothing_new(self) -> None:
        opts = profile_edit.picker_options({}, local=[])
        self.assertNotIn("local", opts)
        self.assertIn("claude", opts)  # harness entries are untouched

    def test_discovery_options_threads_the_probe(self) -> None:
        self.addCleanup(profile_edit._CACHE.clear)
        with mock.patch.object(profiles, "discover", return_value={}), \
             mock.patch.object(profiles, "discover_local",
                               return_value=[{"id": "m", "source": "vllm"}]):
            opts = profile_edit.discovery_options(force=True)
        self.assertEqual(opts["local"]["models"][0]["id"], "m")

    def test_dashboard_carries_the_honesty_line(self) -> None:
        # W-0306 idea 2: the one-line trade on local profiles, exact text.
        html = (Path(__file__).resolve().parent.parent
                / "orchestra" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("This model runs locally: slower per token, "
                      "wins on bounded verifiable ", html)
        self.assertIn("jobs, costs electricity instead of API dollars.", html)
        self.assertIn("localmodels", html)


if __name__ == "__main__":
    unittest.main()
