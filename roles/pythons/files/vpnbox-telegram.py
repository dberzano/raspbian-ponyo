#!/opt/python/globalvenv/bin/pythonglobalvenv.sh

import logging
from pathlib import Path
from typing import Iterable, Optional

from pydantic import BaseModel, ConfigDict, Secret
from telegram import Bot, ForceReply, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


class BotConfig(BaseModel):
    """Configuration schema."""

    telegram_api_key: Secret[str]
    authorized_chat_ids: list[int]
    model_config = ConfigDict(extra="forbid")


# Use this logger for all message printouts
LOGGER: logging.Logger = None

# Configuration
CONF: Optional[BotConfig] = None


# Define a few command handlers. These usually take the two arguments update and
# context.
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    user = update.effective_user
    await update.message.reply_html(
        rf"Hi {user.mention_html()}!",
        reply_markup=ForceReply(selective=True),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /help is issued."""
    await update.message.reply_text("Help!")


async def handle_echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Echo the user message."""
    logger.info(f"echo was called with: {update.message.text}")
    logger.info(f"updater object: {update}")
    await update.message.reply_text(update.message.text)


async def handle_unauthorized(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Echo the user message."""
    logger.warning(f"a message came from an unauthorized party - ignoring: {update}")
    await update.message.reply_text("unauthorized")


class FilterAuthorizedChatId(filters.BaseFilter):

    def __init__(self, authorized_ids: Iterable[int]) -> None:
        """Construct the custom filter."""
        super().__init__("filter_authorized_chat_id", data_filter=False)
        self._authorized_ids = authorized_ids

    def check_update(self, update: Update) -> bool:
        return update.effective_chat.id in self._authorized_ids


async def send_msg_on_start(bot: Bot):
    await bot.send_message(chat_id=AUTHORIZED_CHAT_IDS[0], text="I am up and running!")


def _init_logger() -> None:
    """Initialize logger for all."""
    global LOGGER
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    LOGGER = logging.getLogger("svizzerino")


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
        await context.bot.send_message(job.chat_id, text=f"Hello, the bot just started")

    for cid in CONF.authorized_chat_ids:
        LOGGER.info(f"printing welcome message for chat ID {cid}")
        application.job_queue.run_once(
            _handle_print_event,
            when=0,
            chat_id=cid,
        )


def main():
    """Entry point."""
    _init_logger()
    _init_config()

    LOGGER.info("initializing Telegram bot")
    application = (
        Application.builder().token(CONF.telegram_api_key.get_secret_value()).build()
    )
    authorized = FilterAuthorizedChatId(
        CONF.authorized_chat_ids
    )  # only authorized IDs can chat

    _print_initial_message(application)

    # Blocking call - run all the events and start listening for commands
    application.run_polling(allowed_updates=Update.ALL_TYPES)

    # Print message at start - requires job-queue to be installed

    # # Command handlers (skip if not authorized)
    # application.add_handler(
    #     CommandHandler(command="start", callback=start, filters=authorized)
    # )
    # application.add_handler(
    #     CommandHandler(command="help", callback=help_command, filters=authorized)
    # )

    # # Handle everything else (authorized users only)
    # application.add_handler(
    #     MessageHandler(
    #         filters=(authorized & filters.TEXT & ~filters.COMMAND),
    #         callback=handle_echo,
    #     )
    # )

    # # Handle everything else (not authorized) - note: only one handler will trigger, so this is a fallback
    # application.add_handler(
    #     MessageHandler(
    #         filters=(filters.TEXT & ~filters.COMMAND),
    #         callback=handle_unauthorized,
    #     )
    # )



    # async with application.bot:
    #     application.bot.send_message(chat_id=AUTHORIZED_CHAT_IDS[0], text="I am up and running!")


if __name__ == "__main__":
    # Application's entry point
    main()
