"""Commands the design names that are not built yet."""
from __future__ import annotations


import typer

from .common import app, not_built


@app.command()
def contribute(budget: float = typer.Option(...)):
    """Claim open slots, run them, and submit the records."""
    not_built("Auto mode")


@app.command()
def publish():
    """Release only: publish the selected index tasks to Harbor."""
    not_built("Publishing to Harbor")
