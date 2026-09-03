"""Gradio fixture, deliberately written in the shape that breaks a naive
launcher — the same way `tests/fixtures/fastapi-app` hardcodes a port in
its own `__main__` and `tests/fixtures/mcp-app` is stdio-only.

Two hostile details, both taken from Gradio's own canonical examples:

  - `demo.launch()` is at module level, **not** guarded behind
    `if __name__ == "__main__":`. Importing this module to reach its ASGI
    app would therefore start Gradio's own blocking server and never
    return, unless `Blocks.launch` is neutralized first.
  - it passes an explicit `server_port`, which makes `GRADIO_SERVER_PORT`
    injection silently useless — the script wins.

`sidepage serve` is expected to serve this over its own allocated port
regardless, with nothing ever listening on 8123.
"""

import gradio as gr


def greet(name: str) -> str:
    return f"Hello {name}!"


with gr.Blocks() as demo:
    who = gr.Textbox(label="name")
    greeting = gr.Textbox(label="greeting")
    gr.Button("Greet").click(greet, who, greeting)

demo.launch(server_port=8123)
