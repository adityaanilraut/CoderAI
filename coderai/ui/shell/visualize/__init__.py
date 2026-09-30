"""Bottom dynamic area rendering package.

Holds the terminal bottom-area components that are actually used: renderable
blocks (:mod:`._blocks`), shared Markdown commitment (:mod:`._markdown_boundary`),
fallback streaming (:mod:`._markdown_stream`), tool cards (:mod:`._tool_cards`),
checklists (:mod:`._todos`), interactive overlays (:mod:`._approval_panel`,
:mod:`._question_panel`, :mod:`._btw_panel`), and input routing
(:mod:`._input_router`).

Consumers import the submodules directly, so this package intentionally
re-exports nothing. It is kept as a package marker because setuptools package
discovery (``[tool.setuptools.packages.find]``) uses ``find_packages``, which
requires an ``__init__.py``.
"""
