"""CLI command modules — argument parsing and help text only.

Each module here maps 1:1 to a section of the spec and owns the Typer
wiring for it. None of them implement real behavior; they validate/parse
input, then call into `sidepage.core` (unimplemented) or print a
`sidepage.output.not_implemented` notice naming the future implementation.
"""
