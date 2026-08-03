"""The sidepage SDK — everything that actually does work, as opposed to
`sidepage.commands`, which only parses arguments and prints help text.

Every module in this package is an unimplemented placeholder: class/function
signatures and docstrings describing the intended contract, no logic. Each
`sidepage.commands.*` module names the specific symbol here it will eventually
call, so the seam between "CLI shell" and "SDK" is explicit from day one.
"""
