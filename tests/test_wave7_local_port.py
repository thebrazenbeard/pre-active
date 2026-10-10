import unittest
from pre_active.model_targets import ModelTarget

class LocalModelPortTests(unittest.TestCase):
    def test_unusable_local_port_rejected_before_probe(self):
        for port in ("0", "65536", "notaport"):
            with self.subTest(port=port):
                with self.assertRaisesRegex(ValueError, "port"):
                    ModelTarget(name="loopback",provider="openai-compatible",
                        base_url="http://127.0.0.1:" + port + "/v1",
                        model="qwen",api_key_env=None)
