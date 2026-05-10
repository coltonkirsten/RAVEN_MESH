"""Multi-host federation prototype for RAVEN Mesh.

This package extends the single-host Core with a peer-link protocol that
lets two (or more) Core processes federate so a node on Core A can invoke
a surface on Core B.

The package is a SHIM: it does not modify the production `core/` package.
It builds its own aiohttp app, reusing `core.core.CoreState` and existing
handlers, and adds federation-specific routes and forwarding logic.
"""
