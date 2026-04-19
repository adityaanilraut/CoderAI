#!/usr/bin/env python3
"""Test script to verify CoderAI installation."""

import sys


def test_imports():
    """Test that all required modules can be imported."""
    print("Testing imports...")
    try:
        import rich
        print("✓ rich")
        import click
        print("✓ click")
        import openai
        print("✓ openai")
        import requests
        print("✓ requests")
        import pydantic
        print("✓ pydantic")
        import aiohttp
        print("✓ aiohttp")
        import tiktoken
        print("✓ tiktoken")
        import dotenv
        print("✓ python-dotenv")
        import prompt_toolkit
        print("✓ prompt-toolkit")
        print("\n✓ All dependencies installed correctly!\n")
        return True
    except ImportError as e:
        print(f"\n✗ Import error: {e}")
        print("Please install missing dependencies:")
        print("  pip install -r requirements.txt")
        return False


def test_coderAI_modules():
    """Test that CoderAI modules can be imported."""
    print("Testing CoderAI modules...")
    try:
        from coderAI import __version__
        print(f"✓ coderAI version {__version__}")
        
        from coderAI.config import config_manager
        print("✓ config module")
        
        from coderAI.history import history_manager
        print("✓ history module")
        
        from coderAI.llm import OpenAIProvider, LMStudioProvider
        print("✓ llm module")
        
        from coderAI.tools import ToolRegistry
        print("✓ tools module")
        
        from coderAI.ui import Display
        print("✓ ui module")

        from coderAI.ipc import IPCServer
        print("✓ ipc module")

        from coderAI.binary_manager import ensure_binary
        print("✓ binary_manager module")
        
        from coderAI.agent import Agent
        print("✓ agent module")
        
        print("\n✓ All CoderAI modules loaded successfully!\n")
        return True
    except ImportError as e:
        print(f"\n✗ Module import error: {e}")
        print("Make sure CoderAI is installed:")
        print("  pip install -e .")
        return False


def test_config():
    """Test configuration system."""
    print("Testing configuration...")
    try:
        from coderAI.config import config_manager
        
        # Load config
        config = config_manager.load()
        print(f"✓ Config loaded: {config_manager.config_file}")
        
        # Show some config values
        print(f"  - Default model: {config.default_model}")
        print(f"  - Temperature: {config.temperature}")
        print(f"  - Max tokens: {config.max_tokens}")
        
        print("\n✓ Configuration system working!\n")
        return True
    except Exception as e:
        print(f"\n✗ Config error: {e}")
        return False


def test_tools():
    """Test tool registry."""
    print("Testing tools...")
    try:
        from coderAI.tools import (
            ToolRegistry,
            ReadFileTool,
            WriteFileTool,
            RunCommandTool,
            GitStatusTool,
        )
        
        registry = ToolRegistry()
        registry.register(ReadFileTool())
        registry.register(WriteFileTool())
        registry.register(RunCommandTool())
        registry.register(GitStatusTool())
        
        print(f"✓ Registered {len(registry.get_all())} tools")
        for tool in registry.get_all():
            print(f"  - {tool.name}: {tool.description[:50]}...")
        
        print("\n✓ Tool system working!\n")
        return True
    except Exception as e:
        print(f"\n✗ Tool error: {e}")
        return False


def test_display():
    """Test Rich display."""
    print("Testing Rich display...")
    try:
        from coderAI.ui.display import Display
        
        display = Display()
        display.print("[bold green]✓ Rich display working![/bold green]")
        
        # Test different display methods
        display.print_success("Success message test")
        display.print_info("Info message test")
        display.print_warning("Warning message test")
        
        print()
        return True
    except Exception as e:
        print(f"\n✗ Display error: {e}")
        return False


def main():
    """Run all tests."""
    print("=" * 60)
    print("CoderAI Installation Test")
    print("=" * 60)
    print()
    
    tests = [
        ("Dependencies", test_imports),
        ("CoderAI Modules", test_coderAI_modules),
        ("Configuration", test_config),
        ("Tools", test_tools),
        ("Display", test_display),
    ]
    
    results = []
    for name, test_func in tests:
        try:
            result = test_func()
            results.append((name, result))
        except Exception as e:
            print(f"✗ Test '{name}' crashed: {e}\n")
            results.append((name, False))
    
    print("=" * 60)
    print("Test Results")
    print("=" * 60)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"{status}: {name}")
    
    print()
    print(f"Passed: {passed}/{total}")
    
    if passed == total:
        print("\n🎉 All tests passed! CoderAI is ready to use.")
        print("\nNext steps:")
        print("  1. Run: coderAI setup")
        print("  2. Run: coderAI chat")
        return 0
    else:
        print("\n⚠ Some tests failed. Please check the errors above.")
        print("Try reinstalling:")
        print("  pip install -r requirements.txt")
        print("  pip install -e .")
        return 1


if __name__ == "__main__":
    sys.exit(main())

