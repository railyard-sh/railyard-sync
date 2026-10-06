"""Railyard sync: the shared core of the Railyard NetBox and Nautobot integrations.

- ``railyard_sync.client`` / ``project``: the Railyard REST client and Project JSON wrapper.
- ``railyard_sync.export``: Railyard -> DCIM (the DiffSync source adapter the plugins sync from).
- ``railyard_sync.dcim``: a DCIM site as a source-neutral snapshot, and loaders that read one.
- ``railyard_sync.importer``: DCIM snapshot -> Railyard project, and re-importing it as a baseline.
"""

__version__ = "0.1.0"
