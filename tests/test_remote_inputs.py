"""Viewer input validation, key mapping and controller bookkeeping (RecordingBackend, no OS input)."""

from __future__ import annotations

import unittest

from _bootstrap import PROJECT_DIR
from assistant.remote.inputs import (InputController, InputRejected, RecordingBackend, parse_event,
                                     vk_for)

assert PROJECT_DIR.is_dir()


class ParseEventTests(unittest.TestCase):
    def test_coordinates_are_clamped_into_the_unit_square(self):
        event = parse_event({"k": "move", "x": -5, "y": 9})
        self.assertEqual((event.x, event.y), (0.0, 1.0))

    def test_unknown_kind_and_non_objects_are_rejected(self):
        for raw in ({"k": "shell"}, {"k": "exec", "cmd": "calc"}, [1, 2], "move", None, {}):
            with self.subTest(raw=raw):
                with self.assertRaises(InputRejected):
                    parse_event(raw)

    def test_bad_numbers_and_booleans_are_rejected(self):
        bad = [
            {"k": "move", "x": "0.5", "y": 0.5},
            {"k": "move", "x": True, "y": 0.5},
            {"k": "move", "x": float("nan"), "y": 0.5},
            {"k": "move", "x": None, "y": 0.5},
            {"k": "mdown", "x": 0.1, "y": 0.1, "b": 9},
            {"k": "mdown", "x": 0.1, "y": 0.1, "b": True},
            {"k": "key", "key": "a", "down": "yes"},
            {"k": "key", "key": "", "down": True},
            {"k": "key", "key": "x" * 33, "down": True},
            {"k": "key", "key": "a", "code": "y" * 33, "down": True},
        ]
        for raw in bad:
            with self.subTest(raw=raw):
                with self.assertRaises(InputRejected):
                    parse_event(raw)

    def test_wheel_is_bounded(self):
        self.assertEqual(parse_event({"k": "wheel", "dy": 1e9}).delta, 1000.0)
        self.assertEqual(parse_event({"k": "wheel", "dy": -1e9}).delta, -1000.0)


class KeyMappingTests(unittest.TestCase):
    def test_named_and_coded_keys_map_to_virtual_keys(self):
        self.assertEqual(vk_for("Enter", "Enter"), 0x0D)
        self.assertEqual(vk_for("ArrowLeft", "ArrowLeft"), 0x25)
        self.assertEqual(vk_for("F1", ""), 0x70)
        self.assertEqual(vk_for("F12", ""), 0x7B)
        self.assertEqual(vk_for("a", "KeyA"), ord("A"))
        self.assertEqual(vk_for("5", "Digit5"), ord("5"))
        self.assertEqual(vk_for("-", "Minus"), 0xBD)

    def test_unmapped_keys_are_ignored(self):
        self.assertIsNone(vk_for("Dead", ""))
        self.assertIsNone(vk_for("é", "Unknown"))


class InputControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = RecordingBackend((100, 50, 1000, 600))  # left, top, width, height
        self.controller = InputController(self.backend)

    def test_pointer_maps_into_the_screen_rectangle(self):
        self.controller.apply(parse_event({"k": "move", "x": 0.0, "y": 0.0}))
        self.controller.apply(parse_event({"k": "move", "x": 1.0, "y": 1.0}))
        self.assertEqual(self.backend.calls, [("move", 100, 50), ("move", 1099, 649)])

    def test_clicks_and_wheel_are_forwarded(self):
        self.controller.apply(parse_event({"k": "mdown", "x": 0.5, "y": 0.5, "b": 2}))
        self.controller.apply(parse_event({"k": "mup", "x": 0.5, "y": 0.5, "b": 2}))
        self.controller.apply(parse_event({"k": "wheel", "dy": 100}))
        kinds = [c[0] for c in self.backend.calls]
        self.assertEqual(kinds.count("mouse"), 2)
        self.assertIn(("mouse", 2, True), self.backend.calls)
        self.assertIn(("mouse", 2, False), self.backend.calls)
        self.assertIn(("wheel", -120), self.backend.calls)

    def test_ctrl_chords_are_sent_as_virtual_keys_not_text(self):
        self.controller.apply(parse_event({"k": "key", "key": "c", "code": "KeyC", "down": True,
                                           "ctrl": True}))
        self.controller.apply(parse_event({"k": "key", "key": "c", "code": "KeyC", "down": False,
                                           "ctrl": True}))
        self.assertEqual(self.backend.calls, [("key", ord("C"), True, False),
                                              ("key", ord("C"), False, False)])

    def test_printable_text_uses_unicode_injection(self):
        self.controller.apply(parse_event({"k": "key", "key": "é", "code": "", "down": True}))
        self.assertEqual(self.backend.calls, [("unicode", "é", True)])

    def test_extended_keys_are_flagged(self):
        self.controller.apply(parse_event({"k": "key", "key": "ArrowUp", "code": "ArrowUp", "down": True}))
        self.assertEqual(self.backend.calls, [("key", 0x26, True, True)])

    def test_release_all_clears_every_held_key_and_button(self):
        self.controller.apply(parse_event({"k": "mdown", "x": 0.1, "y": 0.1, "b": 0}))
        self.controller.apply(parse_event({"k": "key", "key": "Shift", "code": "ShiftLeft", "down": True}))
        self.controller.apply(parse_event({"k": "key", "key": "x", "code": "KeyX", "down": True}))
        self.backend.calls.clear()
        self.controller.apply(parse_event({"k": "releaseall"}))
        self.assertCountEqual(self.backend.calls, [("mouse", 0, False),
                                                   ("key", 0x10, False, False),
                                                   ("unicode", "x", False)])
        self.backend.calls.clear()
        self.controller.release_all()
        self.assertEqual(self.backend.calls, [], "a second release sends nothing")

    def test_key_up_without_matching_down_is_ignored(self):
        self.controller.apply(parse_event({"k": "key", "key": "a", "code": "KeyA", "down": False}))
        self.assertEqual(self.backend.calls, [])


if __name__ == "__main__":
    unittest.main()
