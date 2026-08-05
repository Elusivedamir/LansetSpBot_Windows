from __future__ import annotations

from typing import TYPE_CHECKING

from core.account_restriction import get_account_restriction_state

if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:

    class _MixinHost:
        pass


class AccountRestrictionAPIMixin(_MixinHost):
    """Read-only account restriction state exposed to the GUI and schedulers.

    Clearing a restriction is intentionally not exposed as a UI action. The app
    remains fail-closed until the authoritative Telegram-side state is handled
    outside this interface.
    """

    def get_account_restriction_state(self, account_id=None):
        return get_account_restriction_state(self.database, account_id=account_id)
