"""Setup script for CoderAI."""

from setuptools import setup, find_packages

setup(
    name="coderAI",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "rich>=13.7.0",
        "click>=8.1.7",
        "openai>=1.10.0",
        "requests>=2.31.0",
        "pydantic>=2.5.0",
        "aiohttp>=3.9.0",
        "tiktoken>=0.5.2",
        "python-dotenv>=1.0.0",
        "prompt-toolkit>=3.0.43",
    ],
    entry_points={
        "console_scripts": [
            "coderAI=coderAI.cli:main",
        ],
    },
    author="CoderAI",
    author_email="hello@coderai.dev",
    description="A powerful coding agent CLI tool with MCP tools and Rich UI",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    url="https://github.com/yourusername/coderAI",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
    python_requires=">=3.9",
)

