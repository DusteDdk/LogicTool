from __future__ import annotations

import asyncio
import unittest

import mcp.types as types

from logic_mcp.engine import completion
from logic_mcp.engine import get_prompt
from logic_mcp.engine import list_prompts
from logic_mcp.engine import list_resource_templates
from logic_mcp.engine import list_resources
from logic_mcp.engine import list_tools
from logic_mcp.engine import read_resource


class LogicMcpSurfaceMetadataTests(unittest.TestCase):
    def test_tools_include_annotations_and_output_schema(self) -> None:
        result = asyncio.run(list_tools())
        tools = {tool.name: tool for tool in result.tools}

        self.assertIn("logic_list", tools)
        self.assertIn("logic_read", tools)
        self.assertIn("logic_check", tools)
        self.assertIn("logic_context_patch", tools)

        logic_list = tools["logic_list"]
        self.assertIsNotNone(logic_list.annotations)
        self.assertTrue(bool(logic_list.annotations and logic_list.annotations.readOnlyHint))
        self.assertIsNotNone(logic_list.outputSchema)
        self.assertIn("properties", logic_list.inputSchema)
        self.assertIn("detail_level", logic_list.inputSchema["properties"])
        self.assertNotIn("id", logic_list.inputSchema["properties"])
        self.assertNotIn("full", logic_list.inputSchema["properties"]["detail_level"]["enum"])

        logic_read = tools["logic_read"]
        self.assertIsNotNone(logic_read.annotations)
        self.assertTrue(bool(logic_read.annotations and logic_read.annotations.readOnlyHint))
        self.assertIn("id", logic_read.inputSchema["properties"])

        logic_check = tools["logic_check"]
        self.assertIsNotNone(logic_check.outputSchema)
        self.assertIn("result", logic_check.outputSchema.get("properties", {}))

    def test_resources_and_templates_are_exposed(self) -> None:
        resources_result = asyncio.run(list_resources())
        resource_uris = {str(resource.uri) for resource in resources_result.resources}
        self.assertIn("logic://guide/overview", resource_uris)
        self.assertIn("logic://guide/use-cases", resource_uris)
        self.assertIn("logic://session/current/snapshot", resource_uris)

        templates = asyncio.run(list_resource_templates())
        template_uris = {template.uriTemplate for template in templates}
        self.assertIn("logic://session/{session_id}/inventory/{detail_level}", template_uris)
        self.assertIn("logic://session/{session_id}/item/{item_id}", template_uris)

    def test_prompts_and_completion_suggestions(self) -> None:
        prompts_result = asyncio.run(list_prompts())
        prompt_names = {prompt.name for prompt in prompts_result.prompts}
        self.assertIn("logic_orient", prompt_names)
        self.assertIn("logic_capture_discovery", prompt_names)
        self.assertIn("logic_experiment_loop", prompt_names)
        self.assertIn("logic_graph_handoff", prompt_names)

        prompt_result = asyncio.run(get_prompt("logic_orient", {"goal": "verify parity", "certainty": "low"}))
        self.assertTrue(prompt_result.messages)
        first = prompt_result.messages[0]
        self.assertEqual(first.role, "assistant")
        self.assertIsInstance(first.content, types.TextContent)
        self.assertIn("logic_list", first.content.text)

        completion_result = asyncio.run(
            completion(
                types.PromptReference(type="ref/prompt", name="logic_experiment_loop"),
                types.CompletionArgument(name="detail_level", value=""),
                None,
            )
        )
        self.assertIsNotNone(completion_result)
        assert completion_result is not None
        self.assertIn("compact", completion_result.values)
        self.assertIn("full", completion_result.values)

    def test_read_guide_resource(self) -> None:
        contents = list(asyncio.run(read_resource("logic://guide/overview")))
        self.assertEqual(len(contents), 1)
        self.assertIn("Core Operating Pattern", contents[0].content)


if __name__ == "__main__":
    unittest.main()
