"""Synthetic Keychain for offline tests; never modifies a user's real Keychain."""
import contextlib
import functools
import sys
from unittest.mock import patch


class MemoryKeychain:
    def __init__(self):
        self.items = {}

    def get_password(self, service, account):
        return self.items.get((service, account))

    def set_password(self, service, account, password):
        self.items[service, account] = password


def private_storage_test(function):
    """Run business invariants with native DPAPI and simulated macOS Keychain/Fernet.

    macOS runs only the latter by default; native Keychain access is opt-in.
    The Fernet encryption and atomic writes remain real in both cases.
    """
    @functools.wraps(function)
    def run(self):
        modes = ['windows', 'macos'] if sys.platform == 'win32' else ['macos']
        for mode in modes:
            with self.subTest(storage_backend=mode), contextlib.ExitStack() as stack:
                if mode == 'macos':
                    stack.enter_context(patch('common.storage.system', return_value='macos'))
                    stack.enter_context(patch('common.storage._mac_keyring', return_value=MemoryKeychain()))
                function(self)
    return run
