---
name: image-generator
description: Generate a text-to-image file through a user-configured OpenAI-compatible image endpoint. Use when a user asks to create an image and has configured an image API; use CoderAI's /image command instead when attaching an existing image for analysis.
---

# Image Generator

CoderAI does not bundle an image-generation service or account. This optional helper sends a text prompt to an explicitly configured OpenAI-compatible image endpoint and saves the returned image.

`/image` is different: it attaches an existing image to the current CoderAI prompt for analysis.

## Before generating

1. Confirm that the user wants a new image, not analysis of an existing image.
2. Collect the prompt, model, size, and output path. Do not claim support for reference-image editing unless the configured provider and a different client explicitly support it.
3. If the provider charges per request, state that and obtain confirmation before calling it. CoderAI cannot calculate provider pricing.
4. Never ask the user to paste an API key into chat.

## Configuration

Set helper-specific environment variables in the shell:

```bash
export CODERAI_IMAGE_API_URL="https://provider.example/v1/images/generations"
export CODERAI_IMAGE_API_KEY="..."
export CODERAI_IMAGE_MODEL="provider-image-model"
```

The endpoint and model are provider-specific. No vendor URL or model is assumed.

Keep credentials in the process environment or a protected local secret store. Do not put them in command arguments, committed files, logs, or assistant output. Use a narrowly scoped key and rotate it if exposed.

## Generate

```bash
python3 scripts/image_generator.py \
  --prompt "<final prompt>" \
  --size "1024x1024" \
  --output "<target image path>"
```

Optional `--model` overrides `CODERAI_IMAGE_MODEL`. The helper accepts a response containing either `data[0].b64_json` or `data[0].url`.

After success, report only the saved path and the model. On failure, report the sanitized error without printing request headers or credentials.

Do not run this helper when no endpoint is configured. Explain that image generation requires an external provider; do not direct the user to an unverified platform.
