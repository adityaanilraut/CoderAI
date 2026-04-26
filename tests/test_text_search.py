import pytest
from coderAI.tools.search import TextSearchTool

@pytest.fixture
def temp_codebase(tmp_path):
    file_path = tmp_path / "test_file.txt"
    file_path.write_text("class Foo:\n    pass\n")
    return tmp_path

@pytest.mark.asyncio
async def test_text_search_literal(temp_codebase):
    tool = TextSearchTool()
    # Literal search should not match "class \w+"
    result = await tool.execute(query="class \w+", regex=False, base_path=str(temp_codebase))
    assert result["success"] is True
    assert result["count"] == 0

@pytest.mark.asyncio
async def test_text_search_regex(temp_codebase):
    tool = TextSearchTool()
    # Regex search should match "class \w+"
    result = await tool.execute(query="class \w+", regex=True, base_path=str(temp_codebase))
    assert result["success"] is True
    assert result["count"] == 1
    assert result["results"][0]["content"] == "class Foo:"

@pytest.mark.asyncio
async def test_text_search_invalid_regex(temp_codebase):
    tool = TextSearchTool()
    # Malformed regex should return an error, not crash
    result = await tool.execute(query="(unclosed", regex=True, base_path=str(temp_codebase))
    assert result["success"] is False
    assert "Invalid regex" in result["error"]
