"""
Command line interface.

Deliberately empty of imports. The package used to pull in the argparse tree,
which imported every command module, so one broken optional dependency
anywhere took down the whole CLI including ``wasm --version``. The command
tree lives in :mod:`wasm.cli.app` and loads each command module only when that
command is invoked.
"""
