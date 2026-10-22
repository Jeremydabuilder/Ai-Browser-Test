import os

# Tests exercise the AI features, which are off by default in the product.
os.environ.setdefault("PYBROWSER_AI_DEFAULT", "1")
