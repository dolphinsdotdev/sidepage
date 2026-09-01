"""Gradio fixture for the *factory* script shape — the one that broke
sidepage's first Gradio wrapper when a real Space was pulled.

The Blocks is built and returned by a function, and `launch()` is called
on the result inside a `__main__` guard:

    def build_ui(): ...; return demo
    if __name__ == "__main__":
        build_ui().launch()

Nothing at module level ever holds a Blocks, so importing this module and
scanning its namespace finds nothing at all — the failure mode observed
against `huggingface.co/spaces/JacobPEvans/mlx-benchmarks-viewer`. Only
running the file the way `python app.py` would (which is also what Hugging
Face does) reaches the factory call and lets the patched `launch` capture
what it was called on.
"""

import gradio as gr


def echo(text: str) -> str:
    return f"echo: {text}"


def build_ui() -> gr.Blocks:
    with gr.Blocks() as demo:
        said = gr.Textbox(label="say")
        heard = gr.Textbox(label="heard")
        gr.Button("Echo").click(echo, said, heard)
    return demo


# `css=` is passed to `launch()` because Gradio 6 removed it from
# `Blocks`. A host that mounts the Blocks itself must forward it, or the
# app is served unstyled — see `_GRADIO_WRAPPER_SOURCE`'s `_FORWARDED`.
CSS = "#sidepage-css-marker { color: rgb(1, 2, 3); }"

if __name__ == "__main__":
    build_ui().launch(server_port=8124, css=CSS)
