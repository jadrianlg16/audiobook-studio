"""Engine adapters. Each subpackage is COPYed into its own image as ``/app/engine``
(see each Dockerfile), so this file exists only to make ``engines.<id>`` importable
from the repo root for tests — it is never shipped in an image.
"""
