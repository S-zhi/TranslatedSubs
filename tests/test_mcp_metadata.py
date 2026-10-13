import asyncio

from src.mcp_server.server import mcp


def _run(coro):
    return asyncio.run(coro)


def test_server_publishes_agent_instructions_and_tool_descriptions():
    tools = _run(mcp.list_tools())
    tools_by_name = {tool.name: tool for tool in tools}

    assert mcp.description
    assert "check_subtitle_setup" in mcp.instructions
    assert "get_task_artifacts" in mcp.instructions
    assert set(tools_by_name) == {
        "check_subtitle_setup",
        "probe_video",
        "start_subtitle_pipeline",
        "get_task_status",
        "get_task_artifacts",
        "list_tasks",
        "retry_task",
    }
    assert all(tool.description for tool in tools)
    assert "config_file" in tools_by_name["check_subtitle_setup"].description
    assert "get_task_status" in tools_by_name["start_subtitle_pipeline"].description


def test_tool_schema_contains_parameter_descriptions_and_constraints():
    tools = {tool.name: tool for tool in _run(mcp.list_tools())}
    start_schema = tools["start_subtitle_pipeline"].input_schema

    assert start_schema["properties"]["url"]["description"]
    assert start_schema["properties"]["source_lang"]["default"] == "en"
    assert start_schema["properties"]["target_lang"]["default"] == "zh-CN"
    assert start_schema["properties"]["mode"]["enum"] == ["mono", "bilingual"]
    assert start_schema["properties"]["burn"]["enum"] == ["hard", "soft"]
    assert start_schema["properties"]["need_subtitle"]["description"]
    assert tools["list_tasks"].input_schema["properties"]["limit"]["minimum"] == 1
    assert tools["list_tasks"].input_schema["properties"]["limit"]["maximum"] == 200
    assert "offset" in tools["list_tasks"].input_schema["properties"]
    assert "before_id" in tools["list_tasks"].input_schema["properties"]
    assert "after_id" in tools["list_tasks"].input_schema["properties"]


def test_agent_guide_is_discoverable_as_mcp_resource():
    resources = _run(mcp.list_resources())
    guide = next(resource for resource in resources if resource.uri == "subtitles://agent-guide")

    assert guide.name == "subtitle-agent-guide"
    assert guide.mime_type == "text/markdown"

    contents = _run(mcp.read_resource("subtitles://agent-guide"))
    assert len(contents) == 1
    assert "check_subtitle_setup" in contents[0].content
    assert "agent_action=ask_user_to_configure" in contents[0].content
