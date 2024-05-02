#!/opt/python/globalvenv/bin/pythonglobalvenv.sh

import logging
from pathlib import Path
from typing import Iterable, Optional

from pydantic import BaseModel, ConfigDict, Secret
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


class BotConfig(BaseModel):
    """Configuration schema."""

    telegram_api_key: Secret[str]
    authorized_chat_ids: list[int]
    vpn_flavours: list[str]
    model_config = ConfigDict(extra="forbid")


# Use this logger for all message printouts
LOGGER: logging.Logger = None

# Configuration
CONF: Optional[BotConfig] = None


class FilterAuthorizedChatId(filters.BaseFilter):

    def __init__(self, authorized_ids: Iterable[int]) -> None:
        """Construct the custom filter."""
        super().__init__("filter_authorized_chat_id", data_filter=False)
        self._authorized_ids = authorized_ids

    def check_update(self, update: Update) -> bool:
        return update.effective_chat.id in self._authorized_ids


def _init_logger() -> None:
    """Initialize logger for all."""
    global LOGGER

    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    LOGGER = logging.getLogger("svizzerino")
    LOGGER.setLevel(logging.DEBUG)
    svizzerino_handler = logging.StreamHandler()
    svizzerino_handler.setLevel(logging.DEBUG)
    svizzerino_handler.setFormatter(formatter)
    LOGGER.addHandler(svizzerino_handler)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.WARNING)
    root_handler = logging.StreamHandler()
    root_handler.setLevel(logging.WARNING)
    root_handler.setFormatter(formatter)
    root_logger.addHandler(root_handler)


def _init_config() -> None:
    """Load configuration or fail."""
    global CONF
    conf_files = [
        Path.cwd() / ".vpnbox-telegram-config.json",
        Path.home() / ".vpnbox-telegram-config.json",
    ]
    for c in conf_files:
        LOGGER.debug(f"trying to load configuration {c}")
        try:
            CONF = BotConfig.model_validate_json(c.read_text())
            break
        except Exception as e:
            LOGGER.error(f"failed loading {c}: {type(e).__name__} - {str(e)}")
    if CONF is None:
        raise RuntimeError("cannot load configuration")
    LOGGER.info(f"configuration loaded from {c}")


def _print_initial_message(application: Application):
    """Print message at startup using the asyncio paradigm."""

    async def _handle_print_event(context: ContextTypes.DEFAULT_TYPE) -> None:
        """Actually prints the message."""
        job = context.job
        text, markup = _prepare_vpn_menu()
        await context.bot.send_message(job.chat_id, text=text, reply_markup=markup)

    for cid in CONF.authorized_chat_ids:
        LOGGER.info(f"printing welcome message for chat ID {cid}")
        application.job_queue.run_once(
            _handle_print_event,
            when=0,
            chat_id=cid,
        )


def _prepare_vpn_menu() -> tuple[str, ReplyKeyboardMarkup]:
    """Prepare the VPN menu with the buttons."""
    keyboard = [
        [
            InlineKeyboardButton(flavour.upper(), callback_data=flavour)
            for flavour in CONF.vpn_flavours
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    return "Choose the VPN variant you want to connect to:", reply_markup


async def handle_cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    text, markup = _prepare_vpn_menu()
    await update.message.reply_text(text=text, reply_markup=markup)


async def handle_reply_to_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Parses the CallbackQuery and updates the message text."""
    query = update.callback_query

    # CallbackQueries need to be answered, even if no notification to the user is needed
    # Some clients may have trouble otherwise. See https://core.telegram.org/bots/api#callbackquery
    await query.answer()

    await query.edit_message_text(text=f"Selected option: {query.data}")


async def handle_unauthorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all unauthorized users."""
    LOGGER.warning(f"a message came from an unauthorized party - ignoring: {update}")
    await update.message.reply_text("you are not authorized to interact with this bot")


async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all unauthorized users."""
    LOGGER.warning(f"user {update.effective_chat.id} is authorized but wrote something unhandled")
    await update.message.reply_text("I do not understand")


def main():
    """Entry point."""
    _init_logger()
    _init_config()

    LOGGER.info("initializing Telegram bot")
    application = Application.builder().token(CONF.telegram_api_key.get_secret_value()).build()
    authorized = FilterAuthorizedChatId(CONF.authorized_chat_ids)  # only authorized IDs can chat

    # Add a handler for the /start command
    application.add_handler(
        CommandHandler(command="start", callback=handle_cmd_start, filters=authorized)
    )

    # Add a handler for the replies to the start command
    application.add_handler(CallbackQueryHandler(handle_reply_to_start))

    # Handle everything else (unknown)
    application.add_handler(
        MessageHandler(
            filters=(filters.ALL & authorized),
            callback=handle_unknown,
        )
    )

    # Handle everything else (not authorized) - note: only one handler will trigger (fallback)
    application.add_handler(
        MessageHandler(
            filters=(filters.TEXT & ~filters.COMMAND),
            callback=handle_unauthorized,
        )
    )

    _print_initial_message(application)

    # Blocking call - run all the events and start listening for commands
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    # Application's entry point
    main()
