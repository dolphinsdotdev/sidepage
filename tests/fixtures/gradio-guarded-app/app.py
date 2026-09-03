"""Gradio fixture for a script that **never calls `launch()` at all**.

Every Space that works on Hugging Face calls `launch()` somewhere — HF
runs `python app.py` — so this shape is unusual. It exists to exercise
sidepage's last-resort fallback: when running the file produces no
captured Blocks, the wrapper scans the resulting namespace for one.

It also defines a second, unrelated Blocks (`sidebar`) so the fallback
can't pass by simply taking the only one it finds — it has to prefer the
one named `demo`.
"""

import gradio as gr


def shout(text: str) -> str:
    return text.upper()


with gr.Blocks() as sidebar:
    gr.Markdown("a second Blocks that is not the app")

with gr.Blocks() as demo:
    said = gr.Textbox(label="say")
    shouted = gr.Textbox(label="shouted")
    gr.Button("Shout").click(shout, said, shouted)
