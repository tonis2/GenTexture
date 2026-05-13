"""GenTexture pipeline node tree.

A custom Blender NodeTree where the user wires Reference Image, Viewport
Capture, Generate, Project Layer and Output Image nodes to build an AI
texture-generation pipeline. Run it sequentially with the operator
``gentex.run_pipeline``.
"""
