"""
Build configuration for Cython extensions.

Usage:
    uv run python setup.py build_ext --inplace
"""

from setuptools import setup, Extension
from Cython.Build import cythonize

setup(
    name="copilot_cython_utils",
    py_modules=[],  # Don't auto-discover Python modules
    packages=[],    # Don't auto-discover packages
    ext_modules=cythonize(
        [Extension("cython_utils", ["cython_utils.pyx"])],
        compiler_directives={
            'language_level': "3",
            'boundscheck': False,
            'wraparound': False,
            'cdivision': True,
        }
    ),
)
