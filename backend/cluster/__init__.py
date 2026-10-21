"""AI Cluster Gateway: exposes several web-chat providers through an OpenAI-compatible
API by driving browser tabs.

The gateway backend talks to it over HTTP (loopback or another host).
It can also run standalone via `python -m backend.cluster.server`.
"""

__version__ = "2.0.0"
