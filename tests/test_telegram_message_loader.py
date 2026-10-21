"""Exercise production loaders without importing clients, sessions, or workers."""
import ast
import json
from pathlib import Path
import tempfile
import unittest

from message_schema import build_default_language_template, load_message_file


def loader_functions(source_name, messages_path, defaults_path):
    source_path = Path(__file__).resolve().parents[1] / source_name
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    names = {"load_messages"}
    if source_name == "bot.py":
        names.update({"_language_data", "get_response_components"})
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in nodes} != names:
        raise AssertionError("Production message loader functions were not found")
    namespace = {
        "load_message_file": load_message_file,
        "MESSAGES_FILE": messages_path,
        "DEFAULT_MESSAGES_FILE": defaults_path,
        "DEFAULT_LANGUAGE": "es",
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source_path), "exec"), namespace)
    return namespace


class TelegramMessageLoaderTests(unittest.TestCase):
    def test_video_without_audio_survives_both_telegram_loaders(self):
        defaults = {"es": build_default_language_template("es")}
        live = {"es": build_default_language_template("es")}
        for index, response in enumerate((*live["es"]["steps"], live["es"]["call"])):
            response.update(text=f"configured text {index}", audio="", video=f"configured-{index}.mp4")
        with tempfile.TemporaryDirectory() as directory:
            messages_path = Path(directory) / "messages.json"
            defaults_path = Path(directory) / "defaults.json"
            messages_path.write_text(json.dumps(live), encoding="utf-8")
            defaults_path.write_text(json.dumps(defaults), encoding="utf-8")
            # The raw configuration is what the WhatsApp loader consumes.
            raw = json.loads(messages_path.read_text(encoding="utf-8"))
            raw_responses = (*raw["es"]["steps"], raw["es"]["call"])
            before = messages_path.read_bytes()
            for source_name in ("bot.py", "botfather_bot.py"):
                with self.subTest(source=source_name):
                    worker = loader_functions(source_name, messages_path, defaults_path)
                    loaded = worker["load_messages"]()
                    worker["MESSAGES"] = loaded
                    for index, response in enumerate(raw_responses):
                        response_key = ("step1", "step2", "call")[index]
                        expected = (response["text"], response["audio"], response["video"])
                        if source_name == "bot.py":
                            actual = worker["get_response_components"]("es", response_key)
                        elif response_key == "call":
                            call = loaded["es"]["call"]
                            actual = (call["text"], call["audio"], call["video"])
                        else:
                            step = loaded["es"]["steps"][index]
                            actual = (step[0], step[1], step[3])
                        self.assertEqual(expected, actual)
                        self.assertEqual("", actual[1])
            self.assertEqual(before, messages_path.read_bytes())
