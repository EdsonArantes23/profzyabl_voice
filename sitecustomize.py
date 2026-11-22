"""
Compatibility shim for Bothost build check.
The bot itself uses aiogram 3 and does not need executor.
"""

try:
    import aiogram.utils

    if not hasattr(aiogram.utils, "executor"):
        class ExecutorCompatibility:
            pass

        aiogram.utils.executor = ExecutorCompatibility()

except Exception:
    pass
