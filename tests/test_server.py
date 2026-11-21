import asyncio
import importlib
import importlib.util
import unittest


class FakeController:
    def status(self): return {"ok": True, "state": "unresponsive", "completion": {"confirmed": False}}
    def start(self):
        from errors import DriverError
        raise DriverError("wrong_mode", "MEM required", observed_mode=7)


class ServerTests(unittest.TestCase):
    def server(self):
        self.assertIsNotNone(importlib.util.find_spec("server"), "The six-tool stdio server is not implemented")
        return importlib.import_module("server").create_server(FakeController())

    def test_catalog_preserves_six_tools_and_exposes_preparation_extensions(self):
        app = self.server()
        catalog = asyncio.run(app.list_tools())
        tools = {tool.name: tool for tool in catalog}
        self.assertEqual(set(tools), {
            'launch_simulator', 'status', 'start', 'pause', 'resume', 'set_view', 'save_workpiece',
            'setup_status', 'read_program', 'load_program', 'open_workpiece', 'set_mode', 'set_emergency',
            'initialize_controller', 'reset_program', 'open_setup', 'inspect_setup_dialog', 'setup_action',
            'controller_keys', 'capture_screenshot', 'show_program_page', 'controller_program_page', 'rotate_view', 'set_display',
            'inspect_program', 'inspect_workpiece', 'validate_pocket', 'completion_monitor'})
        self.assertTrue(tools["status"].annotations.readOnlyHint)
        self.assertFalse(tools["start"].annotations.readOnlyHint)
        self.assertEqual(tools["set_view"].inputSchema["properties"]["view"]["enum"],
                         ["front", "right", "top", "zoom_in", "zoom_out"])

    def test_status_preserves_unresponsive_result(self):
        result = asyncio.run(self.server().call_tool("status", {}))
        structured = result[1]
        self.assertEqual(structured["state"], "unresponsive")
        self.assertFalse(structured["completion"]["confirmed"])

    def test_expected_rejection_is_structured_and_specific(self):
        result = asyncio.run(self.server().call_tool("start", {}))
        structured = result[1]
        self.assertFalse(structured["ok"])
        self.assertEqual(structured["error"]["code"], "wrong_mode")
        self.assertEqual(structured["error"]["details"]["observed_mode"], 7)
