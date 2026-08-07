import mxlit as mt

mt.title("Hello Mxlit!")
mt.write("This is a simple interactive application.")

if "counter" not in mt.session_state:
    mt.session_state["counter"] = 0

if mt.button("Click me!"):
    mt.session_state["counter"] += 1

mt.write("Button clicked:", mt.session_state["counter"], "times")
