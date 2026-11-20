"""Every command, for a script or a CI job: python -m harbor_master COMMAND. A person uses `hm` and its screens."""
from .cli import app

app()
