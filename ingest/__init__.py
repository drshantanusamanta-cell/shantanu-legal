"""
ingest — document ingestion.

Modules
-------
documents  PDF / DOCX / image ingestion, with vision fallback for scanned pages

IMPORTANT: This file must never be empty. GitHub's web upload interface silently
drops zero-byte files, which causes `ModuleNotFoundError: No module named 'ingest'`
on deployment. Keep content here.

(c) 2026 Dr Shantanu Samanta. All rights reserved.
"""

__all__ = ["documents"]
__version__ = "1.0.0"
