#!/usr/bin/env python3
"""Generate one image through a user-configured OpenAI-compatible endpoint."""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any


class ImageGeneratorError(RuntimeError):
    pass


def build_payload(args: argparse.Namespace) -> dict[str, object]:
    prompt = args.prompt.strip()
    if not prompt:
        raise ImageGeneratorError("Prompt must not be empty.")
    payload: dict[str, object] = {"prompt": prompt}
    if args.model:
        payload["model"] = args.model
    if args.size:
        payload["size"] = args.size
    return payload


def request_image(
    url: str, payload: dict[str, object], api_key: str, timeout: int
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise ImageGeneratorError(f"Image API returned HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise ImageGeneratorError(f"Image API request failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ImageGeneratorError("Image API request timed out.") from exc

    try:
        response_data = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise ImageGeneratorError("Image API returned a non-JSON response.") from exc
    if not isinstance(response_data, dict):
        raise ImageGeneratorError("Image API returned an invalid response object.")
    return response_data


def save_image(response_data: dict[str, Any], output_path: Path) -> Path:
    data = response_data.get("data")
    first = data[0] if isinstance(data, list) and data else None
    if not isinstance(first, dict):
        raise ImageGeneratorError("Image API response is missing data[0].")

    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        encoded = first.get("b64_json")
        image_url = first.get("url")
        if isinstance(encoded, str) and encoded:
            image_data = base64.b64decode(encoded, validate=True)
        elif isinstance(image_url, str) and image_url:
            parsed = urllib.parse.urlparse(image_url)
            if parsed.scheme not in {"http", "https"}:
                raise ImageGeneratorError("Image URL must use HTTP or HTTPS.")
            with urllib.request.urlopen(image_url, timeout=120) as response:
                image_data = response.read()
        else:
            raise ImageGeneratorError("Image API response has neither b64_json nor url.")
        output_path.write_bytes(image_data)
    except ImageGeneratorError:
        raise
    except (ValueError, OSError, urllib.error.URLError, TimeoutError) as exc:
        raise ImageGeneratorError(f"Generated image could not be saved: {exc}") from exc
    return output_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate one image through a configured OpenAI-compatible endpoint.",
    )
    parser.add_argument("--prompt", required=True, help="Image prompt")
    parser.add_argument("--output", required=True, type=Path, help="Output image path")
    parser.add_argument(
        "--endpoint",
        default=os.getenv("CODERAI_IMAGE_API_URL", ""),
        help="Image API URL (default: CODERAI_IMAGE_API_URL)",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("CODERAI_IMAGE_MODEL", ""),
        help="Provider image model (default: CODERAI_IMAGE_MODEL)",
    )
    parser.add_argument(
        "--size",
        default="",
        help="Provider-supported size such as 1024x1024",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=360,
        help="Request timeout in seconds",
    )
    return parser


def run(args: argparse.Namespace) -> dict[str, object]:
    endpoint = args.endpoint.strip()
    api_key = os.getenv("CODERAI_IMAGE_API_KEY", "").strip()
    if not endpoint:
        raise ImageGeneratorError("Set CODERAI_IMAGE_API_URL or pass --endpoint.")
    parsed_endpoint = urllib.parse.urlparse(endpoint)
    if parsed_endpoint.scheme != "https" and parsed_endpoint.hostname not in {
        "localhost",
        "127.0.0.1",
    }:
        raise ImageGeneratorError("Image API URL must use HTTPS, except for localhost.")
    if not api_key:
        raise ImageGeneratorError("Set CODERAI_IMAGE_API_KEY in the process environment.")
    if args.timeout <= 0:
        raise ImageGeneratorError("Timeout must be positive.")

    payload = build_payload(args)
    response_data = request_image(endpoint, payload, api_key, args.timeout)
    output = save_image(response_data, args.output)
    result: dict[str, object] = {
        "output": str(output),
    }
    if args.model:
        result["model"] = args.model
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
    except ImageGeneratorError as exc:
        print(json.dumps({"success": False, "error": str(exc)}), file=sys.stderr)
        return 1

    print(json.dumps({"success": True, "result": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
