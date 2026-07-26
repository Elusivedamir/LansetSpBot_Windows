from __future__ import annotations

from typing import TYPE_CHECKING

from core.account_restriction import (
    clear_account_restriction_after_spambot_confirmation,
    get_account_restriction_state,
)

if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:

    class _MixinHost:
        pass


class AccountRestrictionAPIMixin(_MixinHost):
    def get_account_restriction_state(self, account_id=None):
        return get_account_restriction_state(self.database, account_id=account_id)

    def confirm_spambot_restriction_cleared(self, account_id=None):
        """Clear the local safety lock after the user checked @SpamBot.

        The GUI explicitly asks the user to confirm the bot's current response.
        No text scraping is used because @SpamBot wording may be localized or
        changed by Telegram, and a false positive would silently restart sends.
        """
        return clear_account_restriction_after_spambot_confirmation(
            self.database, account_id=account_id
        )
