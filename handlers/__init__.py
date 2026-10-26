"""Handler modules for tgbot.create_bot.

Each module exposes ``register(bot, tg)``: it declares the same telebot
handlers that used to live as closures inside ``create_bot``, at the same
indentation, with dependencies injected in a short prologue:

* ``bot`` — the TeleBot instance (decorators bind to it directly);
* ``tg`` — the *running* tgbot module object. Passed explicitly rather than
  imported, because ``python tgbot.py`` runs as ``__main__`` and a plain
  ``import tgbot`` would build a second, empty copy of the module with its
  own globals. ``handlers.current_tgbot()`` resolves whichever object is
  actually running.
"""


import sys


def current_tgbot():
    """The tgbot module object that is actually running (tgbot or __main__)."""
    mod = sys.modules.get("tgbot")
    if mod is not None:
        return mod
    main = sys.modules.get("__main__")
    if main is not None and hasattr(main, "create_bot"):
        return main
    raise RuntimeError("tgbot module is not running")
